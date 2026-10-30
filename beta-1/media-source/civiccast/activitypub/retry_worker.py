# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""ActivityPub delivery retry worker (Stage F).

Delivery was production-wired with no retry/backoff/dead-letter: a follower
inbox that was down at publish time simply never heard about the recording.
Failed deliveries (network error or HTTP >= 400) now enqueue a durable
:class:`~civiccast.activitypub.models.DeliveryRetryRecord`; this worker
re-delivers due rows with bounded exponential backoff and dead-letters a row
once ``max_attempts`` is exhausted. Successful retries are recorded in the
normal delivery log so the audit trail stays in one place.

Deployment shape mirrors the finalization worker: ``run_once`` is the
testable unit, ``run_forever`` survives and logs scan exceptions, and the app
lifespan runs the loop on a thread when ``CIVICCAST_ACTIVITYPUB_RETRY_WORKER``
is ``inline`` (the default) and durable storage is active.
"""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from civiccast.activitypub.models import DeliveryRecord, DeliveryRetryRecord
from civiccast.activitypub.remote import ActivityPubDeliveryClient
from civiccast.activitypub.store import ActivityPubStore, new_delivery_id

_LOG = logging.getLogger(__name__)

RETRY_WORKER_MODE_INLINE = "inline"
RETRY_WORKER_MODE_OFF = "off"
_RETRY_WORKER_MODES = (RETRY_WORKER_MODE_INLINE, RETRY_WORKER_MODE_OFF)

__all__ = [
    "ActivityPubRetrySettings",
    "ActivityPubRetryWorker",
    "enqueue_failed_delivery",
]


@dataclass(frozen=True)
class ActivityPubRetrySettings:
    """Deployment configuration for the delivery retry worker."""

    mode: str = RETRY_WORKER_MODE_INLINE
    poll_seconds: float = 60.0
    backoff_seconds: float = 120.0
    max_attempts: int = 8

    @classmethod
    def from_env(cls) -> ActivityPubRetrySettings:
        mode = (
            os.environ.get("CIVICCAST_ACTIVITYPUB_RETRY_WORKER", RETRY_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _RETRY_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_ACTIVITYPUB_RETRY_WORKER must be one of "
                f"{', '.join(_RETRY_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        return cls(
            mode=mode,
            poll_seconds=_env_float(
                "CIVICCAST_ACTIVITYPUB_RETRY_POLL_SECONDS", defaults.poll_seconds
            ),
            backoff_seconds=_env_float(
                "CIVICCAST_ACTIVITYPUB_RETRY_BACKOFF_SECONDS", defaults.backoff_seconds
            ),
            max_attempts=_env_int(
                "CIVICCAST_ACTIVITYPUB_RETRY_MAX_ATTEMPTS", defaults.max_attempts
            ),
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


def enqueue_failed_delivery(
    *,
    store: ActivityPubStore,
    activity_id: str,
    inbox_url: str,
    activity: dict[str, object],
    status_code: int,
    error: str,
    backoff_seconds: float | None = None,
    now: datetime | None = None,
) -> DeliveryRetryRecord:
    """Queue a failed delivery for retry (attempt 1 is the original send)."""

    resolved_now = now or datetime.now(UTC)
    backoff = (
        backoff_seconds
        if backoff_seconds is not None
        else ActivityPubRetrySettings.from_env().backoff_seconds
    )
    record = DeliveryRetryRecord(
        retry_id="apr_" + secrets.token_urlsafe(16).replace("-", "").replace("_", ""),
        activity_id=activity_id,
        inbox_url=inbox_url,
        activity=activity,
        state="pending",
        attempts=1,
        next_attempt_at=resolved_now + timedelta(seconds=backoff),
        last_status_code=status_code,
        last_error=error,
        created_at=resolved_now,
        updated_at=resolved_now,
    )
    _LOG.warning(
        "ActivityPub delivery to %s failed (HTTP %s); queued for retry.",
        inbox_url,
        status_code,
    )
    return store.enqueue_delivery_retry(record)


class ActivityPubRetryWorker:
    """Re-delivers queued failed deliveries with bounded exponential backoff."""

    def __init__(
        self,
        store: ActivityPubStore,
        delivery_client: ActivityPubDeliveryClient,
        *,
        settings: ActivityPubRetrySettings,
    ) -> None:
        self._store = store
        self._client = delivery_client
        self._settings = settings

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
                _LOG.exception("ActivityPub retry scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[DeliveryRetryRecord]:
        """Attempt every due retry once; return the rows that were processed."""

        resolved_now = now or datetime.now(UTC)
        processed: list[DeliveryRetryRecord] = []
        for row in self._store.due_delivery_retries(now=resolved_now):
            processed.append(self._attempt(row, now=resolved_now))
        return processed

    def _attempt(self, row: DeliveryRetryRecord, *, now: datetime) -> DeliveryRetryRecord:
        try:
            result = self._client.deliver(inbox_url=row.inbox_url, activity=row.activity)
            status_code = result.status_code
            error = result.response_body if status_code >= 400 or status_code == 0 else ""
        except Exception as exc:
            status_code = 0
            error = str(exc)
        attempts = row.attempts + 1
        if 0 < status_code < 400:
            self._store.append_delivery(
                DeliveryRecord(
                    delivery_id=new_delivery_id(),
                    activity_id=row.activity_id,
                    inbox_url=row.inbox_url,
                    status_code=status_code,
                    response_body="",
                    created_at=now,
                )
            )
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
                "ActivityPub retry delivered %s to %s (attempt %d).",
                row.activity_id,
                row.inbox_url,
                attempts,
            )
            return self._store.save_delivery_retry(updated)
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
                "ActivityPub delivery to %s dead-lettered after %d attempts (HTTP %s).",
                row.inbox_url,
                attempts,
                status_code,
            )
            return self._store.save_delivery_retry(updated)
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
            "ActivityPub retry to %s failed (HTTP %s, attempt %d/%d); next try in %.0fs.",
            row.inbox_url,
            status_code,
            attempts,
            self._settings.max_attempts,
            delay,
        )
        return self._store.save_delivery_retry(updated)
