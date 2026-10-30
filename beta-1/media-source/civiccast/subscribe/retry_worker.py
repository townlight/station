# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Subscriber webhook delivery retry worker (issue #111).

A real webhook delivery that fails (network error or HTTP >= 400) enqueues a
durable :class:`~civiccast.subscribe.models.WebhookRetryRecord`; this worker
re-delivers due rows with bounded exponential backoff and dead-letters a row
once ``max_attempts`` is exhausted. The queue carries only the subscription id
and payload — the webhook URL and per-subscription secret stay sealed in the
subscriptions table and are reopened here at send time, so a subscription that
unsubscribed after the failure is dead-lettered without ever being called.

Deployment shape mirrors the ActivityPub retry worker: ``run_once`` is the
testable unit, ``run_forever`` survives and logs scan exceptions, and the app
lifespan runs the loop on a thread when ``CIVICCAST_WEBHOOK_RETRY_WORKER`` is
``inline`` (the default) and durable storage is active.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from civiccast.publish.store import PublishStore
from civiccast.subscribe.models import NotificationPayload, WebhookRetryRecord
from civiccast.subscribe.outcome_store import NotificationDeliveryStore
from civiccast.subscribe.secrets import SubscriptionSecrets
from civiccast.subscribe.store import SubscribeStore

_LOG = logging.getLogger(__name__)

RETRY_WORKER_MODE_INLINE = "inline"
RETRY_WORKER_MODE_OFF = "off"
_RETRY_WORKER_MODES = (RETRY_WORKER_MODE_INLINE, RETRY_WORKER_MODE_OFF)

__all__ = [
    "WebhookRetrySettings",
    "WebhookRetryWorker",
    "enqueue_failed_webhook_delivery",
]


class _WebhookClient:
    """Structural protocol stand-in for typing; see delivery.WebhookProvider."""

    def post(self, *, url: str, payload: NotificationPayload, secret: str) -> str:
        raise NotImplementedError


@dataclass(frozen=True)
class WebhookRetrySettings:
    """Deployment configuration for the webhook delivery retry worker."""

    mode: str = RETRY_WORKER_MODE_INLINE
    poll_seconds: float = 60.0
    backoff_seconds: float = 120.0
    max_attempts: int = 8

    @classmethod
    def from_env(cls) -> WebhookRetrySettings:
        mode = (
            os.environ.get("CIVICCAST_WEBHOOK_RETRY_WORKER", RETRY_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _RETRY_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_WEBHOOK_RETRY_WORKER must be one of "
                f"{', '.join(_RETRY_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        return cls(
            mode=mode,
            poll_seconds=_env_float("CIVICCAST_WEBHOOK_RETRY_POLL_SECONDS", defaults.poll_seconds),
            backoff_seconds=_env_float(
                "CIVICCAST_WEBHOOK_RETRY_BACKOFF_SECONDS", defaults.backoff_seconds
            ),
            max_attempts=_env_int("CIVICCAST_WEBHOOK_RETRY_MAX_ATTEMPTS", defaults.max_attempts),
        )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number; got {raw!r}.") from exc


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer; got {raw!r}.") from exc


def enqueue_failed_webhook_delivery(
    *,
    store: SubscribeStore,
    subscription_id: str,
    payload: dict[str, object],
    status_code: int,
    error: str,
    backoff_seconds: float | None = None,
    now: datetime | None = None,
) -> WebhookRetryRecord:
    """Queue a failed webhook delivery for retry (attempt 1 is the original send)."""

    resolved_now = now or datetime.now(UTC)
    backoff = (
        backoff_seconds
        if backoff_seconds is not None
        else WebhookRetrySettings.from_env().backoff_seconds
    )
    record = WebhookRetryRecord(
        retry_id="swr_" + secrets.token_urlsafe(16).replace("-", "").replace("_", ""),
        subscription_id=subscription_id,
        payload=payload,
        state="pending",
        attempts=1,
        next_attempt_at=resolved_now + timedelta(seconds=backoff),
        last_status_code=status_code,
        last_error=error,
        created_at=resolved_now,
        updated_at=resolved_now,
    )
    _LOG.warning(
        "Webhook delivery for subscription %s failed (HTTP %s); queued for retry.",
        subscription_id,
        status_code,
    )
    return store.enqueue_webhook_retry(record)


class WebhookRetryWorker:
    """Re-delivers queued failed webhook deliveries with bounded backoff."""

    def __init__(
        self,
        store: SubscribeStore,
        webhook_client: _WebhookClient,
        subscription_secrets: SubscriptionSecrets,
        *,
        settings: WebhookRetrySettings,
        delivery_store: NotificationDeliveryStore | None = None,
        publish_store: PublishStore | None = None,
    ) -> None:
        self._store = store
        self._client = webhook_client
        self._secrets = subscription_secrets
        self._settings = settings
        self._delivery_store = delivery_store
        self._publish_store = publish_store

    def _record_result(self, row: WebhookRetryRecord, *, now: datetime) -> None:
        if self._delivery_store is None:
            return
        outcome = {"delivered": "sent", "dead_letter": "failed"}.get(row.state, "queued")
        recorded = self._delivery_store.record_webhook_retry_result(
            retry_id=row.retry_id,
            outcome=outcome,  # type: ignore[arg-type]
            detail=(
                "Webhook accepted the HMAC-signed notice after retry."
                if outcome == "sent"
                else row.last_error
            ),
            now=now,
        )
        if recorded is not None and self._publish_store is not None:
            from civiccast.publish.notifications import refresh_notification_surface

            refresh_notification_surface(
                asset_id=recorded.asset_id,
                delivery_store=self._delivery_store,
                publish_store=self._publish_store,
            )

    def run_forever(
        self,
        *,
        poll_seconds: float = 60.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the retry loop until ``stop_event`` is set; survive scan errors."""

        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Webhook retry scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[WebhookRetryRecord]:
        """Attempt every due retry once; return the rows that were processed."""

        resolved_now = now or datetime.now(UTC)
        processed: list[WebhookRetryRecord] = []
        for row in self._store.due_webhook_retries(now=resolved_now):
            processed.append(self._attempt(row, now=resolved_now))
        return processed

    def _attempt(self, row: WebhookRetryRecord, *, now: datetime) -> WebhookRetryRecord:
        subscription = self._store.get(row.subscription_id)
        if (
            subscription is None
            or subscription.status != "confirmed"
            or not subscription.encrypted_webhook_secret
        ):
            # Never call an endpoint whose subscription is gone or unsubscribed.
            updated = row.model_copy(
                update={
                    "state": "dead_letter",
                    "next_attempt_at": None,
                    "last_error": "subscription is no longer confirmed",
                    "updated_at": now,
                }
            )
            _LOG.warning(
                "Webhook retry %s dead-lettered: subscription %s is no longer confirmed.",
                row.retry_id,
                row.subscription_id,
            )
            saved = self._store.save_webhook_retry(updated)
            self._record_result(saved, now=now)
            return saved

        box = self._secrets.secret_box
        url = box.open(subscription.encrypted_subscriber_handle, aad=subscription.subscription_id)
        secret = box.open(
            subscription.encrypted_webhook_secret,
            aad=f"{subscription.subscription_id}:secret",
        )
        # The queued payload deliberately holds no unsubscribe link (a durable
        # queue row should not store a capability token it can rebuild), so the
        # retried notice re-attaches this subscription's own one here. A retry
        # that dropped the unsubscribe link would deliver a notice a resident
        # cannot opt out of.
        from civiccast.publish.targets import resolve_public_base_url
        from civiccast.subscribe.crypto import require_random_webhook_secret
        from civiccast.subscribe.service import unsubscribe_url_for

        payload = NotificationPayload.model_validate(row.payload).model_copy(
            update={
                "unsubscribe_url": unsubscribe_url_for(
                    subscription, base_url=resolve_public_base_url()
                )
            }
        )
        try:
            require_random_webhook_secret(subscription.subscription_id, secret)
            self._client.post(url=url, payload=payload, secret=secret)
            status_code = 200
            error = ""
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", 0)
            error = str(exc)
        attempts = row.attempts + 1
        if not error:
            updated = row.model_copy(
                update={
                    "state": "delivered",
                    "attempts": attempts,
                    "next_attempt_at": None,
                    "last_status_code": status_code,
                    "last_error": "",
                    "updated_at": now,
                }
            )
            _LOG.info(
                "Webhook retry delivered for subscription %s (attempt %d).",
                row.subscription_id,
                attempts,
            )
            saved = self._store.save_webhook_retry(updated)
            self._record_result(saved, now=now)
            return saved
        if attempts >= self._settings.max_attempts:
            updated = row.model_copy(
                update={
                    "state": "dead_letter",
                    "attempts": attempts,
                    "next_attempt_at": None,
                    "last_status_code": status_code,
                    "last_error": error,
                    "updated_at": now,
                }
            )
            _LOG.warning(
                "Webhook delivery for subscription %s dead-lettered after %d attempts (HTTP %s).",
                row.subscription_id,
                attempts,
                status_code,
            )
            saved = self._store.save_webhook_retry(updated)
            self._record_result(saved, now=now)
            return saved
        delay = self._settings.backoff_seconds * (2 ** max(attempts - 1, 0))
        updated = row.model_copy(
            update={
                "attempts": attempts,
                "next_attempt_at": now + timedelta(seconds=delay),
                "last_status_code": status_code,
                "last_error": error,
                "updated_at": now,
            }
        )
        _LOG.warning(
            "Webhook retry for subscription %s failed (HTTP %s, attempt %d/%d); next try in %.0fs.",
            row.subscription_id,
            status_code,
            attempts,
            self._settings.max_attempts,
            delay,
        )
        saved = self._store.save_webhook_retry(updated)
        self._record_result(saved, now=now)
        return saved
