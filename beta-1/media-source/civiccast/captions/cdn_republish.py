# SPDX-License-Identifier: Apache-2.0
# Copyright (c) The CivicCast Authors
"""Push a newly captioned VOD package back to the CDN it is served from.

:func:`civiccast.captions.vod.attach_reviewed_captions` writes to **local
disk**: the segmented WebVTT tracks, the flat English/Spanish sidecars, and
the rewritten multivariant manifest that declares them. For a station
serving VOD from its own portal origin that is the whole job.

For a station with ``CIVICCAST_CDN_PROVIDER`` set, whose package was pushed
to the CDN when it finalized -- *before* caption review finished -- it is
not. The CDN still holds the pre-caption manifest, so residents watching
through the CDN get a recording with no caption button at all while the
operator console says the job completed. That is exactly the shape of
completion claim CivicCast is not allowed to make: an English *and* Spanish
track is an owner requirement for a published recording, and "captioned on
the origin the public does not use" does not meet it.

This module closes that gap by re-uploading only what caption attach
changed -- the caption track playlists and segments, both flat sidecars,
and the rewritten manifest -- through the same
:func:`~civiccast.stream.cdn.package_upload.upload_package_files` helper the
finalization worker publishes with, so the manifest still lands last.

It re-uploads nothing when the station has no record of ever publishing
that package to *this* CDN. See :class:`CdnPackageTarget` for why that is
checked against the recorded manifest URL rather than assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from civiccast.captions.vod import AttachedCaptions
from civiccast.stream.cdn import CDNAdapter
from civiccast.stream.cdn.package_upload import CdnPackageTarget, upload_package_files

_LOG = logging.getLogger(__name__)

__all__ = [
    "CaptionPackageCdnRepublisher",
    "VodPackageCdnRepublisher",
    "caption_artifact_paths",
]


class CaptionPackageCdnRepublisher(Protocol):
    """Contract the offline caption job calls after a successful attach."""

    def republish(
        self,
        *,
        asset_id: str,
        package_dir: Path,
        attached: AttachedCaptions,
    ) -> str | None:
        """Re-publish the captioned package; return the manifest URL.

        Returns ``None`` when this asset's package was never published to
        the configured CDN, which is the normal case for a locally served
        station and is not an error. Raises when a republish was owed and
        failed -- the caption job treats that as a job failure with the
        provider's message on the row, because a green job with a stale CDN
        manifest is a false completion claim.
        """


def caption_artifact_paths(package_dir: Path, attached: AttachedCaptions) -> list[Path]:
    """Return the files caption attach created or rewrote, manifest excluded.

    Deliberately not the whole package tree: the video renditions and their
    segments are byte-identical to what was uploaded at finalization, and a
    council meeting's segments are gigabytes. Only the caption artifacts
    changed, plus the manifest -- which
    :func:`~civiccast.stream.cdn.package_upload.upload_package_files`
    uploads last on its own.
    """

    paths: list[Path] = []
    for output in attached.hls_outputs:
        paths.append(output.playlist_path)
        paths.extend(output.segment_paths)
    paths.append(attached.sidecar_path)
    if attached.spanish_sidecar_path is not None:
        paths.append(attached.spanish_sidecar_path)
    paths.extend(path for path in attached.translated_sidecar_paths.values() if path not in paths)
    return [path for path in paths if path.is_file()]


class VodPackageCdnRepublisher:
    """Re-publish a captioned VOD package through a :class:`CDNAdapter`."""

    def __init__(
        self,
        adapter_provider: Callable[[], CDNAdapter | None],
        target_lookup: Callable[[str], CdnPackageTarget | None],
    ) -> None:
        #: Resolved per call, not captured once: the operator can enter CDN
        #: credentials in the setup wizard after startup, and app.py's
        #: ``resolve_cdn_adapter`` is the seam that picks those up (same
        #: pattern the surge switch and finalization worker use).
        self._adapter_provider = adapter_provider
        self._target_lookup = target_lookup

    def republish(
        self,
        *,
        asset_id: str,
        package_dir: Path,
        attached: AttachedCaptions,
    ) -> str | None:
        adapter = self._adapter_provider()
        if adapter is None:
            return None
        manifest_path = package_dir / "playlist.m3u8"
        target = self._target_lookup(asset_id)
        if target is None or target.recorded_manifest_url is None:
            return None
        manifest_key = f"{target.prefix}/{manifest_path.relative_to(package_dir).as_posix()}"
        if target.recorded_manifest_url != adapter.public_url(manifest_key):
            # The recorded URL is not this CDN's URL for this key: the
            # package was served locally, or from a provider the station has
            # since replaced. Uploading a manifest to a prefix whose media
            # segments were never uploaded would publish a broken package,
            # so do nothing and say so.
            _LOG.info(
                "Asset %s was not published to the configured CDN (recorded manifest URL "
                "%s does not match %s); leaving the CDN copy alone.",
                asset_id,
                target.recorded_manifest_url,
                adapter.public_url(manifest_key),
            )
            return None
        manifest_url = upload_package_files(
            adapter,
            package_dir=package_dir,
            prefix=target.prefix,
            files=caption_artifact_paths(package_dir, attached),
            manifest_path=manifest_path,
        )
        _LOG.info(
            "Re-published the captioned manifest and caption tracks for asset %s to %s.",
            asset_id,
            manifest_url,
        )
        return manifest_url
