# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Retention enforcement worker (Stage F).

Nothing previously acted on ``assets.retention_until`` — retention schedules
were decorative. This worker enforces the *schedule*, not destruction: an
asset whose ``retention_until`` has passed (and whose policy is not
``permanent``) is flagged exactly once into the durable
``asset_disposition_reviews`` queue (migration ``0027``), which records
clerks read via ``GET /api/staff/records/disposition-queue``.

The worker never deletes or mutates assets. CivicCast is a public-records
product: the retention presets ship with "confirm with your records officer"
disclaimers, and automatic purge is an explicit pending product decision —
recorded in the stage plan, the capability matrix, and the result file —
not a silent default.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import DateTime, String, select, text
from sqlalchemy.orm import Mapped, Session, mapped_column

from civiccast.db import Base
from civiccast.schedule.models import Asset

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)

RETENTION_WORKER_MODE_INLINE = "inline"
RETENTION_WORKER_MODE_OFF = "off"
_RETENTION_WORKER_MODES = (RETENTION_WORKER_MODE_INLINE, RETENTION_WORKER_MODE_OFF)

DISPOSITION_STATUS_PENDING_REVIEW = "pending_review"

__all__ = [
    "AssetDispositionReview",
    "DispositionReviewResponse",
    "RetentionEnforcementWorker",
    "RetentionWorkerSettings",
]


class AssetDispositionReview(Base):
    """Append-once flag: this asset's retention schedule has expired."""

    __tablename__ = "asset_disposition_reviews"

    asset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    retention_policy: Mapped[str] = mapped_column(String(20), nullable=False)
    retention_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=DISPOSITION_STATUS_PENDING_REVIEW,
        server_default=DISPOSITION_STATUS_PENDING_REVIEW,
    )
    flagged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP"),
    )


class DispositionReviewResponse(BaseModel):
    """Records-clerk-facing disposition queue row."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)

    asset_id: str
    retention_policy: str
    retention_until: datetime
    status: str
    flagged_at: datetime

    @field_validator("retention_until", "flagged_at")
    @classmethod
    def _aware_utc(cls, value: datetime) -> datetime:
        # SQLite hands back naive datetimes; the stored values are UTC.
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class RetentionWorkerSettings:
    """Deployment configuration for the retention enforcement worker."""

    mode: str = RETENTION_WORKER_MODE_INLINE
    poll_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> RetentionWorkerSettings:
        mode = (
            os.environ.get("CIVICCAST_RETENTION_WORKER", RETENTION_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _RETENTION_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_RETENTION_WORKER must be one of "
                f"{', '.join(_RETENTION_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        raw_poll = os.environ.get("CIVICCAST_RETENTION_POLL_SECONDS", "").strip()
        if not raw_poll:
            poll = defaults.poll_seconds
        else:
            try:
                poll = float(raw_poll)
            except ValueError as exc:
                raise ValueError(
                    f"CIVICCAST_RETENTION_POLL_SECONDS must be a number; got {raw_poll!r}."
                ) from exc
        return cls(mode=mode, poll_seconds=poll)


class RetentionEnforcementWorker:
    """Flags retention-expired assets for records-clerk disposition review."""

    def __init__(
        self, session_factory: SessionFactory, *, settings: RetentionWorkerSettings
    ) -> None:
        self._session_factory = session_factory
        self._settings = settings

    def run_forever(
        self,
        *,
        poll_seconds: float = 3600.0,
        stop_event: threading.Event | None = None,
    ) -> None:
        """Run the scan loop until ``stop_event`` is set; survive scan errors."""

        while stop_event is None or not stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                _LOG.exception("Retention scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[DispositionReviewResponse]:
        """Flag newly expired assets; return only the rows flagged this scan."""

        resolved_now = now or datetime.now(UTC)
        flagged: list[DispositionReviewResponse] = []
        with self._session_factory() as session:
            expired = session.execute(
                select(Asset)
                .where(
                    Asset.retention_until.is_not(None),
                    Asset.retention_until <= resolved_now,
                    Asset.retention_policy != "permanent",
                )
                .order_by(Asset.asset_id.asc())
            ).scalars()
            for asset in expired:
                if asset.retention_until is None:  # guarded by the query; narrows the type
                    continue
                if asset.legal_hold:
                    # S7 media lifecycle / CLAUDE.md §4.6: a legal hold blocks
                    # expiry outright. The asset is never queued for
                    # disposition review while the hold is active, no matter
                    # how far past retention_until it is; clearing the hold
                    # lets the NEXT scan flag it normally.
                    continue
                if session.get(AssetDispositionReview, asset.asset_id) is not None:
                    continue
                review = AssetDispositionReview(
                    asset_id=asset.asset_id,
                    retention_policy=asset.retention_policy,
                    retention_until=asset.retention_until,
                    status=DISPOSITION_STATUS_PENDING_REVIEW,
                    flagged_at=resolved_now,
                )
                session.add(review)
                session.flush()
                session.refresh(review)
                flagged.append(DispositionReviewResponse.model_validate(review))
                _LOG.info(
                    "Asset %s retention expired (%s, until %s); flagged for "
                    "records-clerk disposition review. No automatic deletion.",
                    asset.asset_id,
                    asset.retention_policy,
                    asset.retention_until.isoformat(),
                )
            session.commit()
        return flagged

    def list_disposition_reviews(self) -> list[DispositionReviewResponse]:
        with self._session_factory() as session:
            rows = session.execute(
                select(AssetDispositionReview).order_by(
                    AssetDispositionReview.flagged_at.asc(),
                    AssetDispositionReview.asset_id.asc(),
                )
            ).scalars()
            return [DispositionReviewResponse.model_validate(row) for row in rows]
