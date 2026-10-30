# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Media-library missing-file detection (4.0 scope item 5).

Mirrors :mod:`civiccast.schedule.retention_worker`'s shape exactly (same
env-gated settings dataclass, same ``run_once``/``run_forever`` split, same
``ThreadSupervisor`` wiring in ``civiccast.app``) — that module is the
established pattern for "periodically scan the assets table and flag
something for an operator to act on" in this codebase, and missing-file
detection is the same shape of problem: nothing should be deleted or
rewritten automatically, only flagged.

A file can go missing because an operator moved, renamed, or deleted it
outside CivicCast (a NAS reorganization, a drive swap, manual cleanup).
The worker never touches the filesystem beyond a read-only existence
check, and never mutates the asset's ``file_path`` itself — only its
``file_status``/``file_status_checked_at`` columns. Pointing the asset at
a new path is a separate, explicit operator action
(``POST /api/staff/assets/{asset_id}/relink``).
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
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.schedule.models import FILE_STATUS_MISSING, FILE_STATUS_OK, Asset

SessionFactory = Callable[[], AbstractContextManager[Session]]

_LOG = logging.getLogger(__name__)

MEDIA_INTEGRITY_WORKER_MODE_INLINE = "inline"
MEDIA_INTEGRITY_WORKER_MODE_OFF = "off"
_MEDIA_INTEGRITY_WORKER_MODES = (
    MEDIA_INTEGRITY_WORKER_MODE_INLINE,
    MEDIA_INTEGRITY_WORKER_MODE_OFF,
)

# Mass-missing guard: if more than this fraction of scanned assets would
# flip to missing in a single pass (and at least the floor count), the far
# likelier explanation is a transient storage outage — an unmounted NAS
# share, a disconnected drive — than that many files individually
# vanishing at once. In that case the scan writes NOTHING that pass,
# leaving prior state intact; when the mount returns, the next pass sees
# the files again and proceeds normally (the self-heal-on-reappear
# behavior is unchanged). The absolute floor keeps small libraries
# flaggable: 1-2 genuinely deleted files in a 2-asset library must still
# be flagged, so the guard never fires below the floor.
# ponytail: fixed threshold; make it env-tunable if a real station's
# storage topology needs a different cutoff.
_MASS_MISSING_FRACTION = 0.5
_MASS_MISSING_FLOOR = 3

__all__ = [
    "MediaIntegrityScanResult",
    "MediaIntegrityWorker",
    "MediaIntegrityWorkerSettings",
]


@dataclass(frozen=True)
class MediaIntegrityScanResult:
    """One asset whose status changed during a scan."""

    asset_id: str
    file_status: str


@dataclass(frozen=True)
class MediaIntegrityWorkerSettings:
    """Deployment configuration for the media-integrity scan worker."""

    mode: str = MEDIA_INTEGRITY_WORKER_MODE_INLINE
    poll_seconds: float = 3600.0

    @classmethod
    def from_env(cls) -> MediaIntegrityWorkerSettings:
        mode = (
            os.environ.get("CIVICCAST_MEDIA_INTEGRITY_WORKER", MEDIA_INTEGRITY_WORKER_MODE_INLINE)
            .strip()
            .lower()
        )
        if mode not in _MEDIA_INTEGRITY_WORKER_MODES:
            raise ValueError(
                f"CIVICCAST_MEDIA_INTEGRITY_WORKER must be one of "
                f"{', '.join(_MEDIA_INTEGRITY_WORKER_MODES)}; got {mode!r}."
            )
        defaults = cls()
        raw_poll = os.environ.get("CIVICCAST_MEDIA_INTEGRITY_POLL_SECONDS", "").strip()
        if not raw_poll:
            poll = defaults.poll_seconds
        else:
            try:
                poll = float(raw_poll)
            except ValueError as exc:
                raise ValueError(
                    f"CIVICCAST_MEDIA_INTEGRITY_POLL_SECONDS must be a number; got {raw_poll!r}."
                ) from exc
        return cls(mode=mode, poll_seconds=poll)


class MediaIntegrityWorker:
    """Flags assets whose backing file is gone, and clears stale flags.

    ``run_once`` re-checks every asset that currently has a ``file_path``,
    on every pass (not just previously-``ok`` rows) — an operator restoring
    a file to its recorded path (e.g. reconnecting a NAS share) should see
    the flag clear on the next scan without a manual relink, since the
    original path is valid again and no data changed.
    """

    def __init__(
        self, session_factory: SessionFactory, *, settings: MediaIntegrityWorkerSettings
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
                _LOG.exception("Media integrity scan failed; retrying on the next poll interval.")
            if stop_event is not None:
                stop_event.wait(poll_seconds)
            else:
                time.sleep(poll_seconds)

    def run_once(self, *, now: datetime | None = None) -> list[MediaIntegrityScanResult]:
        """Re-check every asset with a ``file_path``; return rows that changed status.

        A relinked asset (``file_status='relinked'``) is scanned like any
        other — if the newly-linked file also goes missing later, it is
        re-flagged ``missing`` the same way. Assets with no ``file_path``
        (manifest-only assets created via ``POST /api/staff/assets``) are
        skipped entirely; there is nothing on disk for this worker to check.

        Mass-missing guard: if more than ``_MASS_MISSING_FRACTION`` of the
        scanned assets would flip to missing in this one pass (and at least
        ``_MASS_MISSING_FLOOR`` of them), the pass writes nothing and
        returns ``[]`` — see the constants' comment at module top.
        """
        resolved_now = now or datetime.now(UTC)
        changed: list[MediaIntegrityScanResult] = []
        with self._session_factory() as session:
            candidates = list(
                session.execute(
                    select(Asset).where(Asset.file_path.is_not(None)).order_by(Asset.asset_id.asc())
                ).scalars()
            )

            # Phase 1: check every file BEFORE writing anything, so the
            # mass-missing guard can veto the whole pass.
            exists_by_id: dict[str, bool] = {}
            for asset in candidates:
                assert asset.file_path is not None  # guarded by the WHERE clause
                exists_by_id[asset.asset_id] = Path(asset.file_path).is_file()

            newly_missing = sum(
                1
                for asset in candidates
                if not exists_by_id[asset.asset_id] and asset.file_status != FILE_STATUS_MISSING
            )
            if (
                candidates
                and newly_missing >= _MASS_MISSING_FLOOR
                and newly_missing / len(candidates) > _MASS_MISSING_FRACTION
            ):
                _LOG.warning(
                    "Media integrity scan: %d of %d scanned assets would flip to "
                    "missing in one pass — this looks like a storage outage "
                    "(unmounted NAS share, disconnected drive), not individual "
                    "file loss. Skipping all status writes this pass; prior "
                    "state is left intact until the mount returns or an "
                    "operator intervenes.",
                    newly_missing,
                    len(candidates),
                )
                return []

            # Phase 2: apply the per-asset flips (flag missing, clear
            # reappeared) exactly as before.
            for asset in candidates:
                assert asset.file_path is not None
                exists = exists_by_id[asset.asset_id]
                new_status = FILE_STATUS_OK if exists else FILE_STATUS_MISSING
                if new_status == asset.file_status:
                    continue
                asset.file_status = new_status
                asset.file_status_checked_at = resolved_now
                changed.append(
                    MediaIntegrityScanResult(asset_id=asset.asset_id, file_status=new_status)
                )
                if new_status == FILE_STATUS_MISSING:
                    _LOG.warning(
                        "Asset %s backing file not found at %s; flagged missing.",
                        asset.asset_id,
                        asset.file_path,
                    )
                else:
                    _LOG.info(
                        "Asset %s backing file found again at %s; cleared missing flag.",
                        asset.asset_id,
                        asset.file_path,
                    )
            session.commit()
        return changed
