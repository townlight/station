# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Where a finalized live recording's HLS package went on the CDN.

:class:`~civiccast.live.finalization_worker.LiveFinalizationWorker` is the
only writer of CDN-published VOD packages, and it records the outcome on
the session's ``live_finalization_jobs`` row. This module is the *read*
side of that record, kept separate so a consumer can answer "was this
asset's package published to the CDN, and under what key prefix?" without
importing the worker (and, through it, ffprobe, the packager, and the
finalization pipeline).

The consumer that needs it today is the offline caption job: once caption
review completes it rewrites the package's manifest to declare the English
and Spanish tracks, and a CDN-fronted resident sees none of that unless the
rewritten manifest and the new caption files are re-uploaded. See
:mod:`civiccast.captions.cdn_republish`.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from civiccast.live.models import FINALIZATION_STATE_COMPLETED, LiveFinalizationJob
from civiccast.stream.cdn.package_upload import CdnPackageTarget

__all__ = [
    "AssetCdnPackageTargetLookup",
    "build_asset_cdn_package_target_lookup",
    "live_package_cdn_prefix",
]

#: ``asset_id`` -> where its package was published, or ``None`` when this
#: station has no record of publishing that asset's package at all.
AssetCdnPackageTargetLookup = Callable[[str], CdnPackageTarget | None]


def live_package_cdn_prefix(live_session_id: str) -> str:
    """Return the CDN key prefix a live session's package is published under.

    Single definition of the convention, shared by the uploader
    (:meth:`civiccast.live.finalization_worker.LiveFinalizationWorker
    ._upload_package`) and by every later reader, so a republish can never
    guess a prefix that diverges from the one the files actually went to.
    """

    return f"live/{quote(live_session_id)}"


def build_asset_cdn_package_target_lookup(
    session_factory: Callable[[], AbstractContextManager[Session]] | Any,
) -> AssetCdnPackageTargetLookup:
    """Return a lookup from ``asset_id`` to its published-package target.

    Returns ``None`` for an asset with no *completed* finalization job --
    which is every uploaded-and-published asset today, since uploads are
    packaged locally and never pushed to a CDN. That ``None`` is the honest
    answer "this station never published this package to a CDN", and the
    caption republisher treats it as nothing to do rather than as an error.
    """

    def lookup(asset_id: str) -> CdnPackageTarget | None:
        with session_factory() as session:
            # ``asset_id`` is a plain nullable column on live_finalization_jobs
            # with no unique constraint -- the primary key is
            # ``live_session_id`` -- so two completed sessions can name the
            # same asset (a re-finalize after a repackage, for one). An
            # earlier ``scalar_one_or_none()`` here raised MultipleResultsFound
            # on that shape, turning a survivable ambiguity into a caption-job
            # failure. Order by completion and take the most recent instead:
            # the newest completed package is the one whose files are on the
            # CDN now, and therefore the one whose manifest a caption
            # republish must rewrite. ``completed_at`` can be NULL on an older
            # row, so ``live_session_id`` breaks the tie deterministically
            # rather than leaving the choice to row order.
            row = session.execute(
                select(LiveFinalizationJob)
                .where(
                    LiveFinalizationJob.asset_id == asset_id,
                    LiveFinalizationJob.state == FINALIZATION_STATE_COMPLETED,
                )
                .order_by(
                    LiveFinalizationJob.completed_at.desc().nullslast(),
                    LiveFinalizationJob.live_session_id.desc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if row is None:
                return None
            return CdnPackageTarget(
                prefix=live_package_cdn_prefix(row.live_session_id),
                recorded_manifest_url=row.package_manifest_url,
            )

    return lookup
