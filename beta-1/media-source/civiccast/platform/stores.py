# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""App-owned store bundle helpers for FastAPI router dependencies."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from fastapi import HTTPException, Request, status

StoreFactory = Callable[[], Any]


def _missing_optional_store() -> None:
    return None


@dataclass(frozen=True)
class AppStoreBundle:
    """Store factories owned by one FastAPI app instance."""

    asset_store: StoreFactory
    caption_review_store: StoreFactory
    summary_store: StoreFactory
    record_store: StoreFactory
    publish_store: StoreFactory
    subscribe_store: StoreFactory
    podcast_store: StoreFactory
    activitypub_store: StoreFactory
    analytics_store: StoreFactory = _missing_optional_store
    #: Offline caption job queue (K3). Optional: a station running without
    #: durable storage has nowhere to keep a job that spans an operator's
    #: review, so the publish path skips enqueueing rather than pretending.
    caption_job_store: StoreFactory = _missing_optional_store
    #: Async summary generation job queue. Optional for the same reason as
    #: caption_job_store: a station without durable storage has nowhere to
    #: keep a job across a multi-minute CPU generation (field evidence
    #: 2026-08-29 -- see civiccast/summary/job.py).
    summary_job_store: StoreFactory = _missing_optional_store
    #: Durable per-recipient subscriber-notification receipts (WP-05). Optional
    #: for the same reason as the job queues: an app instance with no durable
    #: storage has nowhere to keep a delivery receipt, and the publish path
    #: falls back to a process-local guard rather than claiming a receipt it
    #: cannot keep.
    notification_delivery_store: StoreFactory = _missing_optional_store
    #: Schedule/live-finalization channel association used by the canonical
    #: publication-target resolver (WP-05). Optional: without it an asset
    #: resolves to the station's default channel rather than failing.
    publication_target_lookup: StoreFactory = _missing_optional_store


def resolve_app_store(request: Request, name: str, *, surface: str) -> Any:
    """Resolve one app-owned store factory or fail with an actionable 503."""

    bundle = getattr(request.app.state, "store_bundle", None)
    factory = getattr(bundle, name, None)
    if bundle is None or factory is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"{surface} is not configured for this app instance. "
                "Restart CivicCast through create_app() or configure the store bundle."
            ),
        )
    return cast(StoreFactory, factory)()
